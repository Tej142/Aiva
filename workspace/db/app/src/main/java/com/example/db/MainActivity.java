package com.example.db;
import android.database.sqlite.SQLiteDatabase;
import android.os.Bundle;
import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import android.widget.*;
import android.view.View;
import android.database.Cursor;


public class MainActivity extends AppCompatActivity {

    EditText et1,et2,et3;
    Button ins,dis,upt,del;
    TextView tv;

    SQLiteDatabase db;
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        EdgeToEdge.enable(this);
        setContentView(R.layout.activity_main);

        et1 = findViewById(R.id.et1);
        et2 = findViewById(R.id.et2);
        et3 = findViewById(R.id.et3);
        ins = findViewById(R.id.ins);
        upt = findViewById(R.id.upt);
        dis = findViewById(R.id.dis);
        del = findViewById(R.id.del);

        tv = findViewById(R.id.tv);

        db = openOrCreateDatabase("stu",MODE_PRIVATE,null);
        db.execSQL("CREATE TABLE IF NOT EXISTS stu (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)");
        ins.setOnClickListener(new View.OnClickListener(){
            public void onClick(View v){
                String id = et1.getText().toString();
                String name = et2.getText().toString();
                String age = et3.getText().toString();

                db.execSQL("INSERT INTO stu VALUES("+id+",'"+name+"',"+age+")");
                et1.setText("");
                et2.setText("");
                et3.setText("");
            }
        });
        upt.setOnClickListener(new View.OnClickListener(){
            public void onClick(View v){
                String id = et1.getText().toString();
                String name = et2.getText().toString();
                String age = et3.getText().toString();
                db.execSQL("UPDATE stu SET name ='" +name + "', age = "+age+"  WHERE id= "+id);
                et1.setText("");
                et2.setText("");
                et3.setText("");
            }
        });
        del.setOnClickListener(new View.OnClickListener(){
            public void onClick(View v){
                String id = et1.getText().toString();
                db.execSQL("DELETE FROM stu WHERE id=" +id);
                et1.setText("");
                et2.setText("");
                et3.setText("");
            }
        });
        dis.setOnClickListener(new View.OnClickListener(){
            public void onClick(View v){
                Cursor c = db.rawQuery("SELECT * FROM stu",null);
                tv.setText("");
                while(c.moveToNext()){
                    tv.append("ID: "+c.getInt(0)+"\n Name:"+c.getString(1)+"\n Age:"+c.getInt(2)+"\n\n");

                }
                c.close();
            }
        });



    }
}