package com.example.dateandtime;

import android.os.Bundle;

import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import android.widget.*;

import org.w3c.dom.Text;
import android.view.View;
import java.sql.Time;

public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        EdgeToEdge.enable(this);
        setContentView(R.layout.activity_main);

        DatePicker dt = findViewById(R.id.dt);
        TextView tv = findViewById(R.id.tv);

        dt.setOnDateChangedListener(new DatePicker.OnDateChangedListener(){
            public void onDateChanged(DatePicker view, int y, int m, int d){
                Toast.makeText(MainActivity.this, "The updated Date is: "+ y + "/" + (m+1)+"/"+ d,Toast.LENGTH_LONG).show();
            }
        });

    }
}